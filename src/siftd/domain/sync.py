"""Domain models for sync operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SyncRemote:
    """A registered sync remote."""

    name: str
    host: str | None  # None for local-path remotes
    path: str
    last_push: str | None = None  # ISO 8601 timestamp


@dataclass
class PushResult:
    """Result of a push operation."""

    conversations: int
    size_bytes: int
    remote_name: str
    remote_existed: bool
    dry_run: bool
    last_push_updated: bool = False
