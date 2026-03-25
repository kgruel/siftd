"""Domain models for sync operations."""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Sync wire protocol version — bump when the push/pull stream format changes.
# Follows the same pattern as ADAPTER_INTERFACE_VERSION for system boundaries.
SYNC_PROTOCOL_VERSION = 1

# Capabilities advertised by the receiver via sync-status.
# New features are capabilities, not version bumps.
SYNC_CAPABILITIES: frozenset[str] = frozenset({"staged"})

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
class SyncFilters:
    """Per-remote default filters for push/pull slicing."""

    workspace: str | None = None
    tag: list[str] | None = None
    no_tag: list[str] | None = None
    owner: str | None = None


@dataclass
class SyncRemote:
    """A registered sync remote."""

    name: str
    host: str | None  # None for local-path remotes
    path: str
    last_push: str | None = None  # ISO 8601 timestamp
    last_pull: str | None = None  # ISO 8601 timestamp
    last_sent: str | None = None  # ISO 8601 — most recent staged delivery
    strategy: str = "incremental"  # "incremental" | "full"
    filters: SyncFilters | None = None


    @classmethod
    def from_config(cls, cfg: dict) -> SyncRemote:
        """Build a SyncRemote from a config dict (as returned by get_sync_remote).

        Converts the ``filters`` sub-dict to a SyncFilters instance and drops
        keys that aren't SyncRemote fields (e.g. ``auth``).
        """
        cfg = dict(cfg)  # don't mutate caller's dict
        cfg.pop("auth", None)
        filters_raw = cfg.pop("filters", None)
        if isinstance(filters_raw, dict):
            cfg["filters"] = SyncFilters(**filters_raw)
        return cls(**cfg)


@dataclass
class SyncStatus:
    """Receiver capabilities and inbox state from a pre-flight check."""

    capabilities: frozenset[str]
    inbox_pending: int = 0
    inbox_total: int = 0
    protocol_version: int = SYNC_PROTOCOL_VERSION

    @classmethod
    def from_json(cls, data: dict) -> SyncStatus:
        """Parse a sync-status JSON response."""
        return cls(
            capabilities=frozenset(data.get("capabilities", [])),
            inbox_pending=data.get("inbox", {}).get("pending", 0),
            inbox_total=data.get("inbox", {}).get("total", 0),
            protocol_version=data.get("protocol_version", SYNC_PROTOCOL_VERSION),
        )


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
