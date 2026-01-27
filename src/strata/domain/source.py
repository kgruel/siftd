"""Source abstraction for adapter inputs."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Source:
    """Represents an input source for adapters.

    Abstracts over different input types (files, directories, databases)
    so adapters can declare what they handle and consumers can route
    appropriately.
    """
    kind: str  # "file", "sqlite", "directory"
    location: Path | str
    metadata: dict = field(default_factory=dict)

    @property
    def as_path(self) -> Path:
        """Return location as a Path object."""
        return Path(self.location)
