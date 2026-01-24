"""Protocol definitions for adapters and storage."""

from typing import Iterable, Protocol

from .models import Conversation
from .source import Source


class Adapter(Protocol):
    """Protocol for log adapters.

    Adapters are pure parsers: they read from a Source and yield
    Conversation domain objects. No storage coupling.
    """
    name: str
    default_locations: list[str]
    source_kinds: list[str]

    def can_handle(self, source: Source) -> bool:
        """Return True if this adapter can parse the given source."""
        ...

    def parse(self, source: Source) -> Iterable[Conversation]:
        """Parse the source and yield Conversation objects."""
        ...


class Storage(Protocol):
    """Protocol for conversation storage backends."""

    def store_conversation(self, conversation: Conversation) -> str:
        """Store a conversation, returning its internal ID."""
        ...
