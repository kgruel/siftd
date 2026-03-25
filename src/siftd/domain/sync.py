"""Domain models for sync operations."""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Sync wire protocol version — bump when the push/pull stream format changes.
# Follows the same pattern as ADAPTER_INTERFACE_VERSION for system boundaries.
SYNC_PROTOCOL_VERSION = 1

# 6-byte magic prefix for sync streams.
SYNC_MAGIC = b"SIFTD\x00"

# Full header: magic (6) + version (2 big-endian) = 8 bytes.
SYNC_HEADER = SYNC_MAGIC + struct.pack(">H", SYNC_PROTOCOL_VERSION)


def parse_sync_header(data: bytes) -> int | None:
    """Parse the protocol version from an 8-byte sync header.

    Returns the version number if the magic matches, or None if the data
    doesn't start with the sync magic (indicating an old sender).
    """
    if len(data) < 8 or data[:6] != SYNC_MAGIC:
        return None
    return struct.unpack(">H", data[6:8])[0]


@dataclass
class SyncRemote:
    """A registered sync remote."""

    name: str
    host: str | None  # None for local-path remotes
    path: str
    last_push: str | None = None  # ISO 8601 timestamp
    last_pull: str | None = None  # ISO 8601 timestamp


@dataclass
class PushResult:
    """Result of a push operation."""

    conversations: int
    size_bytes: int
    remote_name: str
    remote_existed: bool
    dry_run: bool
    last_push_updated: bool = False


@dataclass
class PullResult:
    """Result of a pull operation."""

    conversations: int
    size_bytes: int
    remote_name: str
    dry_run: bool
    last_pull_updated: bool = False
