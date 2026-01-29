"""Ingestion layer for siftd."""

from typing import Any

from .discovery import discover_all
from .orchestration import IngestStats, ingest_all

# Adapter modules have NAME, DEDUP_STRATEGY, parse(), etc. as module-level attributes.
# Using Any since Python doesn't have a clean type for "module with specific attributes".
AdapterModule = Any

__all__ = ["AdapterModule", "discover_all", "ingest_all", "IngestStats"]
